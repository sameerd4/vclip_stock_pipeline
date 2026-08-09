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
from .collections import CollectionService, CollectionSuggestion, load_rule
from .enrichment import VisualEnrichmentService
from .export_ingest import ExportIngestService
from .frames import FrameSampler
from .providers import OpenAIVisualAnalyzer
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
    search.add_argument("--json", action="store_true")
    search.set_defaults(handler=_run_catalog_search)

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
    if args.html:
        print(f"Review HTML:        {args.html}")
    return 0


def _run_catalog_reindex(args: argparse.Namespace) -> int:
    _, workflow = _catalog(args.db)
    count = workflow.rebuild_search_index()
    print(f"Indexed {count} exported clip(s).")
    return 0


def _run_catalog_search(args: argparse.Namespace) -> int:
    _, workflow = _catalog(args.db)
    rows = workflow.search(args.query, limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        return 0
    for row in rows:
        tags = ", ".join(item["tag"] for item in row.get("tags", [])[:6])
        markets = ", ".join(item["market_label"] for item in row.get("markets", []))
        print(f"{row['stock_clip_id']}  {markets or '-'}  {tags or '-'}")
        if row.get("caption"):
            print(f"    {row['caption']}")
        print(f"    {row['exported_path']}")
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
