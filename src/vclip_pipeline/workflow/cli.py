"""Command line for the post-Stockify workflow layer."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from ..db import CatalogRepository, Database
from ..errors import VClipError
from .catalog import WorkflowCatalog
from .catalog_quality import (
    CatalogQualityService,
    format_quality_report,
    write_quality_report,
)
from .collections import CollectionService, CollectionSuggestion, load_rule
from .enrichment import VisualEnrichmentService, format_openai_usage_block
from .entities import EntityCatalog
from .export_ingest import ExportIngestService
from .frames import FrameSampler
from .providers import OpenAIVisualAnalyzer
from .review_dedupe import ReviewDedupeService, format_text_report
from .review_dedupe_batch import ReviewDedupeBatchService, format_batch_text_report
from .review_dedupe_global import ReviewGlobalDedupeService, format_global_text_report
from .review_color_integrity import (
    ReviewColorIntegrityService,
    format_color_integrity_text,
)
from .review_color_repair import (
    ReviewColorRepairService,
    format_color_repair_text,
)
from .review_location_materialize import (
    ReviewLocationMaterializeService,
    format_materialize_plan_text,
)
from .review_location_recover import (
    ReviewLocationRecoverService,
    format_location_recover_text,
)
from .review_prune import ReviewPruneService, format_prune_text_report
from .review_shard import ReviewShardService
from .taxonomy import VisualTaxonomy


def _data_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "data" / name


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _progress(quiet: bool):
    started = time.monotonic()

    def emit(message: str) -> None:
        if not quiet:
            print(f"[{time.monotonic() - started:7.1f}s] {message}", flush=True)

    return emit


def _catalog(db_path: Path) -> tuple[CatalogRepository, WorkflowCatalog]:
    database = Database(db_path.expanduser().resolve())
    database.migrate()
    repository = CatalogRepository(database)
    return repository, WorkflowCatalog(database)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vclip-workflow",
        description=(
            "Post-Stockify review sharding, export ingest, visual enrichment, "
            "catalog search, and collection materialization."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    shard = sub.add_parser(
        "review-shard",
        help="Split an existing Stockify review XML without rerunning Stockify.",
    )
    shard.add_argument("review_xml", type=Path)
    shard.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    shard.add_argument("--output", type=Path, required=True)
    shard.add_argument(
        "--group-by", choices=("market", "event", "none"), default="market"
    )
    shard.add_argument(
        "--representation",
        choices=("individual", "compilation", "both"),
        default="individual",
    )
    shard.add_argument("--max-projects", type=_positive_int, default=125)
    shard.add_argument("--max-megabytes", type=_positive_float, default=8.0)
    shard.add_argument("--markets", type=Path, default=_data_path("workflow_markets.json"))
    shard.add_argument("--include-compilations", action="store_true")
    shard.add_argument(
        "--no-scope-markers",
        action="store_true",
        help=(
            "Omit tiny source-project scope markers. Not recommended: if every "
            "individual clip from one source project is deleted, Reconcile may be "
            "unable to infer that the deleted projects were in this partial XML."
        ),
    )
    shard.add_argument("--overwrite", action="store_true")
    shard.add_argument("--dry-run", action="store_true")
    shard.add_argument("--report", type=Path)
    shard.add_argument("--quiet", action="store_true")
    shard.set_defaults(handler=_run_review_shard)

    dedupe = sub.add_parser(
        "review-dedupe",
        help=(
            "Remove exact source-range duplicate projects from a review shard "
            "FCPXML without rerunning Stockify."
        ),
    )
    dedupe.add_argument("input_xml", type=Path)
    dedupe.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    dedupe.add_argument("--output", type=Path, required=True)
    dedupe.add_argument("--report", type=Path, required=True)
    dedupe.add_argument("--text-report", type=Path, required=True)
    dedupe.add_argument(
        "--manifest",
        type=Path,
        help="Optional shard manifest; defaults to <input>-shard-manifest.json.",
    )
    dedupe.add_argument("--overwrite", action="store_true")
    dedupe.add_argument("--dry-run", action="store_true")
    dedupe.add_argument("--quiet", action="store_true")
    dedupe.set_defaults(handler=_run_review_dedupe)

    dedupe_batch = sub.add_parser(
        "review-dedupe-batch",
        help=(
            "Bulk exact source-range duplicate removal across a review-shard "
            "corpus, preferring portable XML with manifests from a separate root."
        ),
    )
    dedupe_batch.add_argument("--input-root", type=Path, required=True)
    dedupe_batch.add_argument("--manifest-root", type=Path, required=True)
    dedupe_batch.add_argument("--output-root", type=Path, required=True)
    dedupe_batch.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    dedupe_batch.add_argument("--report", type=Path, required=True)
    dedupe_batch.add_argument("--text-report", type=Path, required=True)
    dedupe_batch.add_argument(
        "--near-policy",
        choices=("none", "aggressive"),
        default="none",
        help=(
            "none: exact dedupe only (default). "
            "aggressive: also remove near-duplicates with containment>=0.95 and IoU>=0.92."
        ),
    )
    dedupe_batch.add_argument("--overwrite", action="store_true")
    dedupe_batch.add_argument("--dry-run", action="store_true")
    dedupe_batch.add_argument("--quiet", action="store_true")
    dedupe_batch.set_defaults(handler=_run_review_dedupe_batch)

    dedupe_global = sub.add_parser(
        "review-dedupe-global",
        help=(
            "Remove duplicate candidate representations across clean shard "
            "boundaries, keeping one canonical stock candidate per source-range asset."
        ),
    )
    dedupe_global.add_argument("--input-root", type=Path, required=True)
    dedupe_global.add_argument("--output-root", type=Path, required=True)
    dedupe_global.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    dedupe_global.add_argument("--report", type=Path, required=True)
    dedupe_global.add_argument("--text-report", type=Path, required=True)
    dedupe_global.add_argument("--conflict-report", type=Path, required=True)
    dedupe_global.add_argument(
        "--near-policy",
        choices=("none", "aggressive"),
        default="none",
        help=(
            "none: exact cross-shard dedupe only (default). "
            "aggressive: also collapse cross-shard near-duplicates."
        ),
    )
    dedupe_global.add_argument("--overwrite", action="store_true")
    dedupe_global.add_argument("--dry-run", action="store_true")
    dedupe_global.add_argument("--quiet", action="store_true")
    dedupe_global.set_defaults(handler=_run_review_dedupe_global)

    prune = sub.add_parser(
        "review-prune",
        help=(
            "Remove unusably short individual stock candidates from a canonical "
            "review shard corpus."
        ),
    )
    prune.add_argument("--input-root", type=Path, required=True)
    prune.add_argument("--output-root", type=Path, required=True)
    prune.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    prune.add_argument(
        "--min-duration",
        type=_positive_float,
        default=3.0,
        help="Remove candidates with effective duration strictly below this value (seconds).",
    )
    prune.add_argument("--report", type=Path, required=True)
    prune.add_argument("--text-report", type=Path, required=True)
    prune.add_argument("--overwrite", action="store_true")
    prune.add_argument("--dry-run", action="store_true")
    prune.add_argument("--quiet", action="store_true")
    prune.set_defaults(handler=_run_review_prune)

    color_integrity = sub.add_parser(
        "review-color-integrity",
        help=(
            "Read-only audit of Custom LUT / effect presence across a final "
            "review shard corpus versus DB camera_lut metadata, with optional "
            "SRT color_md D-Log M Camera LUT integrity analysis."
        ),
    )
    color_integrity.add_argument("--input-root", type=Path, required=True)
    color_integrity.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    color_integrity.add_argument("--report", type=Path, required=True)
    color_integrity.add_argument("--text-report", type=Path, required=True)
    color_integrity.add_argument(
        "--csv-report",
        type=Path,
        help="Optional CSV path for definitive color_md=dlog_m candidate rows.",
    )
    color_integrity.add_argument(
        "--media-root",
        type=Path,
        action="append",
        default=[],
        help=(
            "Media/SRT root to scan for color_md (repeatable). Required for the "
            "D-Log M Camera LUT integrity section."
        ),
    )
    color_integrity.add_argument("--quiet", action="store_true")
    color_integrity.set_defaults(handler=_run_review_color_integrity)

    color_repair = sub.add_parser(
        "review-color-repair",
        help=(
            "Repair the confirmed Mini 5 Pro ← Air 3 wrong Camera LUT cohort "
            "in a review shard corpus (color_md=dlog_m only)."
        ),
    )
    color_repair.add_argument("--input-root", type=Path, required=True)
    color_repair.add_argument("--output-root", type=Path, required=True)
    color_repair.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    color_repair.add_argument(
        "--media-root",
        type=Path,
        action="append",
        required=True,
        help="Media/SRT root to scan for color_md (repeatable).",
    )
    color_repair.add_argument("--report", type=Path, required=True)
    color_repair.add_argument("--text-report", type=Path, required=True)
    color_repair.add_argument("--overwrite", action="store_true")
    color_repair.add_argument("--dry-run", action="store_true")
    color_repair.add_argument("--quiet", action="store_true")
    color_repair.set_defaults(handler=_run_review_color_repair)

    locate = sub.add_parser(
        "review-location-recover",
        help=(
            "Recover Unknown Location labels in a final review shard corpus "
            "from SRT GPS under explicit media roots. Optional "
            "--forensic-jpg-exif runs a read-only JPG EXIF same-shoot pass."
        ),
    )
    locate.add_argument("--input-root", type=Path, required=True)
    locate.add_argument(
        "--output-root",
        type=Path,
        help="Required unless --forensic-jpg-exif (read-only forensic mode).",
    )
    locate.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    locate.add_argument(
        "--media-root",
        type=Path,
        action="append",
        required=True,
        help=(
            "Media/SRT/JPG root to scan (repeatable). Scanned once for .SRT "
            "and, in forensic mode, nearby .JPG/.JPEG stills."
        ),
    )
    locate.add_argument("--report", type=Path, required=True)
    locate.add_argument("--text-report", type=Path, required=True)
    locate.add_argument(
        "--forensic-jpg-exif",
        action="store_true",
        help=(
            "Read-only forensic mode: for Unknown sources lacking usable SRT "
            "GPS, correlate nearby DJI JPG/JPEG EXIF GPS as jpg_exif_same_shoot "
            "evidence. Does not mutate XML or DB."
        ),
    )
    locate.add_argument(
        "--places-file",
        type=Path,
        help="Optional local places catalog JSON (defaults to bundled places.json).",
    )
    locate.add_argument(
        "--location-provider",
        choices=("catalog", "catalog+nominatim"),
        default="catalog",
    )
    locate.add_argument("--nominatim-user-agent")
    locate.add_argument(
        "--location-overrides",
        type=Path,
        help=(
            "JSON file of geographic-cluster overrides keyed by cluster_id or "
            "original_event + representative GPS. Applies only to clusters with "
            "valid recovered GPS."
        ),
    )
    locate.add_argument("--overwrite", action="store_true")
    locate.add_argument("--dry-run", action="store_true")
    locate.add_argument("--quiet", action="store_true")
    locate.set_defaults(handler=_run_review_location_recover)

    materialize = sub.add_parser(
        "review-location-materialize",
        help=(
            "Materialize persisted JPG EXIF / editorial-group forensic location "
            "knowledge into a new shard root without remounting media."
        ),
    )
    materialize.add_argument("--input-root", type=Path, required=True)
    materialize.add_argument("--output-root", type=Path, required=True)
    materialize.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    materialize.add_argument(
        "--forensic-json",
        type=Path,
        required=True,
        help="Persisted jpg-exif-forensic.json report to consume.",
    )
    materialize.add_argument(
        "--projected-coverage-json",
        type=Path,
        help="Optional projected-drone-location-coverage.json for cross-checks.",
    )
    materialize.add_argument(
        "--plan-json",
        type=Path,
        required=True,
        help="Write location-materialization-plan.json here.",
    )
    materialize.add_argument(
        "--plan-text",
        type=Path,
        required=True,
        help="Write location-materialization-plan.txt here.",
    )
    materialize.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Plan and audit only; do not write the output shard root or DB (default).",
    )
    materialize.add_argument(
        "--write",
        action="store_true",
        help="Actually write the output shard root and persist DB updates.",
    )
    materialize.add_argument("--overwrite", action="store_true")
    materialize.add_argument(
        "--skip-color-integrity",
        action="store_true",
        help="Skip the read-only color-integrity check during dry-run/write.",
    )
    materialize.add_argument(
        "--refresh-audit",
        action="store_true",
        help=(
            "Rebuild the materialization plan/audit against an already-written "
            "output root without mutating XML or DB."
        ),
    )
    materialize.add_argument("--quiet", action="store_true")
    materialize.set_defaults(handler=_run_review_location_materialize)

    ingest = sub.add_parser(
        "exports-ingest",
        help="Match and persist final exports without creating packages.",
    )
    ingest.add_argument("exports_directory", type=Path)
    ingest.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    ingest.add_argument("--run-id")
    ingest.add_argument("--project-label", action="append", default=[])
    ingest.add_argument("--no-checksum", action="store_true")
    ingest.add_argument("--no-probe", action="store_true")
    ingest.add_argument("--allow-unmatched", action="store_true")
    ingest.add_argument("--allow-missing", action="store_true")
    ingest.add_argument("--allow-duration-mismatch", action="store_true")
    ingest.add_argument("--allow-unreconciled", action="store_true")
    ingest.add_argument("--dry-run", action="store_true")
    ingest.add_argument("--report", type=Path)
    ingest.add_argument("--quiet", action="store_true")
    ingest.set_defaults(handler=_run_exports_ingest)

    enrich = sub.add_parser(
        "enrich",
        help="Extract representative frames and optionally analyze visible content.",
    )
    enrich.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    enrich.add_argument("--run-id")
    enrich.add_argument("--cache", type=Path, required=True)
    enrich.add_argument("--provider", choices=("frames-only", "openai"), default="frames-only")
    enrich.add_argument("--model", default="gpt-5-mini")
    enrich.add_argument("--taxonomy", type=Path, default=_data_path("visual_taxonomy.json"))
    enrich.add_argument("--markets", type=Path, default=_data_path("workflow_markets.json"))
    enrich.add_argument("--include-pending", action="store_true")
    enrich.add_argument("--limit", type=_positive_int)
    enrich.add_argument("--force", action="store_true")
    enrich.add_argument("--fail-fast", action="store_true")
    enrich.add_argument("--dry-run", action="store_true")
    enrich.add_argument("--report", type=Path)
    enrich.add_argument("--html", type=Path)
    enrich.add_argument("--quiet", action="store_true")
    enrich.set_defaults(handler=_run_enrich)

    catalog = sub.add_parser("catalog", help="Build and query the canonical clip search index.")
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)
    reindex = catalog_sub.add_parser("reindex")
    reindex.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    reindex.set_defaults(handler=_run_catalog_reindex)
    search = catalog_sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    search.add_argument("--limit", type=_positive_int, default=50)
    search.add_argument(
        "--explain",
        action="store_true",
        help="Show score contributions for each result.",
    )
    search.add_argument("--json", action="store_true")
    search.set_defaults(handler=_run_catalog_search)
    audit = catalog_sub.add_parser(
        "audit",
        help="Audit visual-enrichment quality for a run/cohort (no OpenAI calls).",
    )
    audit.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    audit.add_argument("--run-id")
    audit.add_argument("--report", type=Path)
    audit.add_argument("--no-diagnostics", action="store_true")
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(handler=_run_catalog_audit)

    catalog_audit = sub.add_parser(
        "catalog-audit",
        help="Alias for 'catalog audit': visual enrichment quality report.",
    )
    catalog_audit.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    catalog_audit.add_argument("--run-id")
    catalog_audit.add_argument("--report", type=Path)
    catalog_audit.add_argument("--no-diagnostics", action="store_true")
    catalog_audit.add_argument("--json", action="store_true")
    catalog_audit.set_defaults(handler=_run_catalog_audit)

    canonicalize = sub.add_parser(
        "canonicalize-entities",
        help=(
            "Resolve persisted named-subject suggestions to canonical entities "
            "without calling OpenAI or re-extracting frames."
        ),
    )
    canonicalize.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    canonicalize.add_argument("--run-id")
    canonicalize.add_argument(
        "--entities",
        type=Path,
        default=_data_path("visual_entities.json"),
    )
    canonicalize.add_argument("--dry-run", action="store_true")
    canonicalize.add_argument("--report", type=Path)
    canonicalize.set_defaults(handler=_run_canonicalize_entities)

    collections = sub.add_parser("collections", help="Suggest and freeze sellable clip sets.")
    collection_sub = collections.add_subparsers(dest="collection_command", required=True)
    suggest = collection_sub.add_parser("suggest")
    suggest.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    suggest.add_argument("--title", required=True)
    suggest.add_argument("--slug")
    suggest.add_argument("--description")
    suggest.add_argument("--rule", type=Path, required=True)
    suggest.add_argument("--output", type=Path, required=True)
    suggest.add_argument("--publish", action="store_true")
    suggest.set_defaults(handler=_run_collection_suggest)
    publish = collection_sub.add_parser("publish")
    publish.add_argument("suggestion", type=Path)
    publish.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    publish.set_defaults(handler=_run_collection_publish)
    materialize = collection_sub.add_parser("materialize")
    materialize.add_argument("slug")
    materialize.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    materialize.add_argument("--output", type=Path, required=True)
    materialize.add_argument("--version", type=_positive_int)
    materialize.add_argument("--mode", choices=("copy", "hardlink", "symlink"), default="hardlink")
    materialize.add_argument("--overwrite", action="store_true")
    materialize.add_argument("--dry-run", action="store_true")
    materialize.set_defaults(handler=_run_collection_materialize)
    return parser


def _run_review_shard(args: argparse.Namespace) -> int:
    repository, _ = _catalog(args.db)
    output = args.output.expanduser().resolve()
    report_path = args.report or output / "review-shard-report.json"
    result = ReviewShardService(repository, progress=_progress(args.quiet)).run(
        review_xml=args.review_xml.expanduser().resolve(),
        output_directory=output,
        markets_path=args.markets.expanduser().resolve(),
        group_by=args.group_by,
        representation=args.representation,
        max_projects=args.max_projects,
        max_megabytes=args.max_megabytes,
        include_scope_markers=not args.no_scope_markers,
        include_compilations=args.include_compilations,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        report_path=report_path,
    )
    print()
    print("Review sharding complete")
    print("------------------------")
    print(f"Run ID:           {result.stockify_run_id}")
    print(f"Projects selected:{result.projects_selected:>7}")
    print(f"Shards:           {result.shards_written:>7}")
    print(f"Output:           {result.output_directory}")
    print(f"Report:           {report_path}")
    return 0


def _run_review_dedupe(args: argparse.Namespace) -> int:
    repository, workflow = _catalog(args.db)
    report = ReviewDedupeService(
        repository,
        workflow,
        progress=_progress(args.quiet),
    ).run(
        input_xml=args.input_xml.expanduser().resolve(),
        output_xml=args.output.expanduser().resolve(),
        report_path=args.report.expanduser().resolve(),
        text_report_path=args.text_report.expanduser().resolve(),
        manifest_path=(
            args.manifest.expanduser().resolve() if args.manifest else None
        ),
        dry_run=args.dry_run,
        overwrite=args.overwrite,
    )
    print()
    print(format_text_report(report).rstrip())
    print(f"JSON report: {args.report}")
    print(f"Text report: {args.text_report}")
    return 0


def _run_review_dedupe_batch(args: argparse.Namespace) -> int:
    repository, workflow = _catalog(args.db)
    report = ReviewDedupeBatchService(
        repository,
        workflow,
        progress=_progress(args.quiet),
    ).run(
        input_root=args.input_root.expanduser().resolve(),
        manifest_root=args.manifest_root.expanduser().resolve(),
        output_root=args.output_root.expanduser().resolve(),
        report_path=args.report.expanduser().resolve(),
        text_report_path=args.text_report.expanduser().resolve(),
        dry_run=args.dry_run,
        overwrite=args.overwrite,
        near_policy=args.near_policy,
    )
    print()
    print(format_batch_text_report(report).rstrip())
    print(f"JSON report: {args.report}")
    print(f"Text report: {args.text_report}")
    return 1 if report.shards_failed else 0


def _run_review_dedupe_global(args: argparse.Namespace) -> int:
    repository, workflow = _catalog(args.db)
    report = ReviewGlobalDedupeService(
        repository,
        workflow,
        progress=_progress(args.quiet),
    ).run(
        input_root=args.input_root.expanduser().resolve(),
        output_root=args.output_root.expanduser().resolve(),
        report_path=args.report.expanduser().resolve(),
        text_report_path=args.text_report.expanduser().resolve(),
        conflict_report_path=args.conflict_report.expanduser().resolve(),
        near_policy=args.near_policy,
        dry_run=args.dry_run,
        overwrite=args.overwrite,
    )
    print()
    print(format_global_text_report(report).rstrip())
    print(f"JSON report:     {args.report}")
    print(f"Text report:     {args.text_report}")
    print(f"Conflict report: {args.conflict_report}")
    return 1 if report.shards_failed else 0


def _run_review_prune(args: argparse.Namespace) -> int:
    repository, workflow = _catalog(args.db)
    report = ReviewPruneService(
        repository,
        workflow,
        progress=_progress(args.quiet),
    ).run(
        input_root=args.input_root.expanduser().resolve(),
        output_root=args.output_root.expanduser().resolve(),
        report_path=args.report.expanduser().resolve(),
        text_report_path=args.text_report.expanduser().resolve(),
        min_duration=args.min_duration,
        dry_run=args.dry_run,
        overwrite=args.overwrite,
    )
    print()
    print(format_prune_text_report(report).rstrip())
    print(f"JSON report: {args.report}")
    print(f"Text report: {args.text_report}")
    return 1 if report.shards_failed else 0


def _run_review_color_integrity(args: argparse.Namespace) -> int:
    repository, _workflow = _catalog(args.db)
    report = ReviewColorIntegrityService(
        repository,
        progress=_progress(args.quiet),
    ).run(
        input_root=args.input_root.expanduser().resolve(),
        report_path=args.report.expanduser().resolve(),
        text_report_path=args.text_report.expanduser().resolve(),
        media_roots=[path.expanduser().resolve() for path in (args.media_root or [])],
        csv_report_path=(
            args.csv_report.expanduser().resolve() if args.csv_report else None
        ),
    )
    print()
    print(format_color_integrity_text(report).rstrip())
    print(f"JSON report: {args.report}")
    print(f"Text report: {args.text_report}")
    if report.csv_report_path:
        print(f"CSV report:  {report.csv_report_path}")
    if report.dlog_audit:
        counts = report.dlog_audit.get("classification_counts") or {}
        print(
            "D-Log M audit: "
            f"{report.dlog_audit.get('dlog_m_candidates', 0)} candidates, "
            f"{counts.get('DLOG_CORRECT_CAMERA_LUT', 0)} correct, "
            f"{counts.get('DLOG_NO_CAMERA_LUT', 0)} no camera LUT, "
            f"{len(report.camera_lut_signatures)} distinct LUT signatures"
        )
    parse_failures = sum(
        1 for item in report.failures if item.get("status") == "xml_parse_error"
    )
    return 1 if parse_failures else 0


def _run_review_color_repair(args: argparse.Namespace) -> int:
    repository, workflow = _catalog(args.db)
    report = ReviewColorRepairService(
        repository,
        workflow,
        progress=_progress(args.quiet),
    ).run(
        input_root=args.input_root.expanduser().resolve(),
        output_root=args.output_root.expanduser().resolve(),
        media_roots=[path.expanduser().resolve() for path in args.media_root],
        report_path=args.report.expanduser().resolve(),
        text_report_path=args.text_report.expanduser().resolve(),
        dry_run=args.dry_run,
        overwrite=args.overwrite,
    )
    print()
    print(format_color_repair_text(report).rstrip())
    print(f"JSON report: {args.report}")
    print(f"Text report: {args.text_report}")
    if report.post_write_audit:
        print(
            "Post-audit: "
            f"still_wrong={report.post_write_audit.get('still_wrong_camera_lut')} "
            f"db_xml_mismatches={report.post_write_audit.get('db_xml_mismatches')}"
        )
    return 1 if report.shards_failed else 0


def _run_review_location_materialize(args: argparse.Namespace) -> int:
    repository, workflow = _catalog(args.db)
    dry_run = not bool(args.write)
    if args.refresh_audit and args.write:
        raise VClipError("--refresh-audit cannot be combined with --write")
    report = ReviewLocationMaterializeService(
        repository,
        workflow,
        progress=_progress(args.quiet),
    ).run(
        input_root=args.input_root.expanduser().resolve(),
        output_root=args.output_root.expanduser().resolve(),
        forensic_json=args.forensic_json.expanduser().resolve(),
        projected_coverage_json=(
            args.projected_coverage_json.expanduser().resolve()
            if args.projected_coverage_json
            else None
        ),
        plan_json=args.plan_json.expanduser().resolve(),
        plan_text=args.plan_text.expanduser().resolve(),
        dry_run=dry_run,
        overwrite=bool(args.overwrite),
        skip_color_integrity=bool(args.skip_color_integrity),
        refresh_audit=bool(args.refresh_audit),
    )
    print()
    print(format_materialize_plan_text(report).rstrip())
    print(f"Plan JSON: {args.plan_json}")
    print(f"Plan text: {args.plan_text}")
    checks = report.dry_run_checks or {}
    if (report.dry_run or args.refresh_audit) and not checks.get("all_passed"):
        return 1
    return 1 if report.shards_failed else 0


def _run_review_location_recover(args: argparse.Namespace) -> int:
    from ..geo import build_location_resolver, default_places_path

    repository, workflow = _catalog(args.db)
    places_path = (
        args.places_file.expanduser().resolve()
        if args.places_file
        else default_places_path()
    )
    location_resolver = build_location_resolver(
        repository,
        places_path=places_path,
        enable_nominatim=args.location_provider == "catalog+nominatim",
        nominatim_user_agent_override=args.nominatim_user_agent,
    )
    if not args.forensic_jpg_exif and args.output_root is None:
        raise VClipError("--output-root is required unless --forensic-jpg-exif")
    report = ReviewLocationRecoverService(
        repository,
        location_resolver,
        workflow,
        progress=_progress(args.quiet),
    ).run(
        input_root=args.input_root.expanduser().resolve(),
        output_root=(
            args.output_root.expanduser().resolve() if args.output_root else None
        ),
        media_roots=[path.expanduser().resolve() for path in args.media_root],
        report_path=args.report.expanduser().resolve(),
        text_report_path=args.text_report.expanduser().resolve(),
        dry_run=args.dry_run,
        overwrite=args.overwrite,
        location_overrides=(
            args.location_overrides.expanduser().resolve()
            if args.location_overrides
            else None
        ),
        forensic_jpg_exif=bool(args.forensic_jpg_exif),
    )
    print()
    print(format_location_recover_text(report).rstrip())
    print(f"JSON report: {args.report}")
    print(f"Text report: {args.text_report}")
    return 1 if report.shards_failed else 0


def _run_exports_ingest(args: argparse.Namespace) -> int:
    repository, workflow = _catalog(args.db)
    report_path = args.report or args.exports_directory.with_name(
        f"{args.exports_directory.name}-ingest-report.json"
    )
    result = ExportIngestService(
        repository,
        workflow,
        progress=_progress(args.quiet),
    ).run(
        exports_directory=args.exports_directory.expanduser().resolve(),
        run_id=args.run_id,
        project_labels=set(args.project_label) if args.project_label else None,
        calculate_checksums=not args.no_checksum,
        inspect_media=not args.no_probe,
        allow_unmatched=args.allow_unmatched,
        allow_missing=args.allow_missing,
        allow_duration_mismatch=args.allow_duration_mismatch,
        allow_unreconciled=args.allow_unreconciled,
        dry_run=args.dry_run,
        report_path=report_path,
    )
    print()
    print("Export ingest complete")
    print("----------------------")
    print(f"Run ID:          {result.stockify_run_id}")
    print(f"Matched:         {result.exports_matched}")
    print(f"Persisted:       {result.exports_persisted}")
    print(f"Missing:         {len(result.missing_candidate_ids)}")
    print(f"Report:          {report_path}")
    return 0


def _run_enrich(args: argparse.Namespace) -> int:
    _, workflow = _catalog(args.db)
    taxonomy = VisualTaxonomy.from_path(args.taxonomy.expanduser().resolve())
    from .review_shard import MarketCatalog

    markets = MarketCatalog.from_path(args.markets.expanduser().resolve())
    analyzer = (
        OpenAIVisualAnalyzer(taxonomy=taxonomy, model=args.model)
        if args.provider == "openai"
        else None
    )
    service = VisualEnrichmentService(
        workflow,
        FrameSampler(args.cache),
        taxonomy,
        markets,
        analyzer,
        progress=_progress(args.quiet),
    )
    result = service.run(
        run_id=args.run_id,
        include_pending=args.include_pending,
        limit=args.limit,
        force=args.force,
        fail_fast=args.fail_fast,
        dry_run=args.dry_run,
        report_path=args.report,
        html_path=args.html,
    )
    print()
    print("Visual enrichment complete")
    print("--------------------------")
    print(f"Exports considered: {result.exports_considered}")
    print(f"Cached:             {result.cached}")
    print(f"Analyzed:           {result.analyzed}")
    print(f"Failed:             {result.failed}")
    usage_lines = format_openai_usage_block(result)
    if usage_lines:
        print()
        for line in usage_lines:
            print(line)
    for warning in result.warnings:
        print(f"warning: {warning}")
    if args.html:
        print(f"Review HTML:        {args.html}")
    return 0


def _run_catalog_reindex(args: argparse.Namespace) -> int:
    _, workflow = _catalog(args.db)
    count = workflow.rebuild_search_index()
    print(f"Indexed {count} exported clip(s).")
    return 0


def _run_catalog_audit(args: argparse.Namespace) -> int:
    _, workflow = _catalog(args.db)
    service = CatalogQualityService(workflow)
    report = service.audit(
        run_id=args.run_id,
        include_diagnostics=not args.no_diagnostics,
    )
    if args.report:
        write_quality_report(args.report.expanduser().resolve(), report)
        print(f"Report: {args.report}")
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        for line in format_quality_report(report):
            print(line)
    return 0


def _run_canonicalize_entities(args: argparse.Namespace) -> int:
    _, workflow = _catalog(args.db)
    entities = EntityCatalog.from_path(args.entities.expanduser().resolve())
    service = CatalogQualityService(workflow, entities=entities)
    result = service.canonicalize_entities(
        run_id=args.run_id,
        dry_run=args.dry_run,
    )
    if args.report:
        path = args.report.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Report: {path}")
    print("Entity canonicalization")
    print("-----------------------")
    print(f"Clips considered:   {result['clips_considered']}")
    print(f"Clips updated:      {result['clips_updated']}")
    print(f"Subjects resolved:  {result['subjects_resolved']}")
    print(f"Subjects unresolved:{result['subjects_unresolved']}")
    if args.dry_run:
        print("Dry run:            no database writes")
    return 0


def _run_catalog_search(args: argparse.Namespace) -> int:
    _, workflow = _catalog(args.db)
    rows = workflow.search(
        args.query,
        limit=args.limit,
        explain=bool(args.explain),
    )
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        return 0
    for row in rows:
        tags = ", ".join(item["tag"] for item in row.get("tags", [])[:6])
        markets = ", ".join(item["market_label"] for item in row.get("markets", []))
        score = row.get("search_score")
        score_text = f"  score={score:.1f}" if isinstance(score, (int, float)) else ""
        print(f"{row['stock_clip_id']}{score_text}  {markets or '-'}  {tags or '-'}")
        if row.get("caption"):
            print(f"    {row['caption']}")
        print(f"    {row['exported_path']}")
        if args.explain:
            explanation = row.get("search_explain") or {}
            for item in explanation.get("contributions", []):
                points = item.get("points", 0.0)
                detail = f" ({item['detail']})" if item.get("detail") else ""
                print(
                    f"    +{points:.1f}  {item.get('kind')}  "
                    f"{item.get('label')}{detail}"
                )
    print(f"\nResults: {len(rows)}")
    return 0


def _run_collection_suggest(args: argparse.Namespace) -> int:
    _, workflow = _catalog(args.db)
    service = CollectionService(workflow)
    suggestion = service.suggest(
        title=args.title,
        slug=args.slug,
        description=args.description,
        rule=load_rule(args.rule),
    )
    payload = asdict(suggestion)
    if args.publish:
        payload["published"] = service.publish(suggestion)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Suggested {suggestion.selected_count}/{suggestion.candidate_count} clip(s).")
    print(f"Output: {args.output}")
    return 0


def _suggestion_from_path(path: Path) -> CollectionSuggestion:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return CollectionSuggestion(
        title=str(payload["title"]),
        slug=str(payload["slug"]),
        description=payload.get("description"),
        rule=dict(payload["rule"]),
        candidate_count=int(payload["candidate_count"]),
        selected_count=int(payload["selected_count"]),
        clips=list(payload["clips"]),
    )


def _run_collection_publish(args: argparse.Namespace) -> int:
    _, workflow = _catalog(args.db)
    result = CollectionService(workflow).publish(
        _suggestion_from_path(args.suggestion)
    )
    print(json.dumps(result, indent=2))
    return 0


def _run_collection_materialize(args: argparse.Namespace) -> int:
    _, workflow = _catalog(args.db)
    result = CollectionService(workflow).materialize(
        slug=args.slug,
        output_directory=args.output,
        version=args.version,
        mode=args.mode,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    print(f"Materialized {result['clip_count']} clip(s).")
    print(f"Output: {result['output_directory']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (VClipError, FileExistsError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
