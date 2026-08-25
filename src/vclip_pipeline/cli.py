"""Terminal interface for Stockify, Reconcile, and Package."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from .db import CatalogRepository, Database
from .errors import VClipError
from .geo import build_location_resolver, default_places_path
from .packaging import PackageService
from .packaging.weather import (
    NoWeatherProvider,
    OpenMeteoHistoricalWeatherProvider,
)
from .reconcile import ReconcileService
from .stockify import StockifyOptions, StockifyService
from .stockify.fcpxml import (
    print_asset_diagnostics,
    print_validation_report,
    resolve_input_fcpxml,
)
from .stockify.libraries import (
    discover_xml_library_names,
    find_fcpbundles,
    format_libraries_report,
)
from .stockify.location_diagnose import (
    LocationDiagnosticsService,
    format_location_diagnostics_report,
)
from .stockify.location_recovery import (
    LocationRecoveryService,
    format_location_recovery_report,
)
from .stockify.models import StockifyError


def positive_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("Value must be zero or greater.")
    return number


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Value must be greater than zero.")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vclip",
        description=(
            "Turn finished Final Cut Pro libraries into reviewed, reconciled, "
            "metadata-rich stock footage packages."
        ),
    )
    parser.add_argument("--version", action="version", version="vclip 0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_stockify_parser(subparsers)
    _add_reconcile_parser(subparsers)
    _add_package_parser(subparsers)
    _add_recover_locations_parser(subparsers)
    _add_diagnose_locations_parser(subparsers)
    _add_libraries_parser(subparsers)
    _add_db_parser(subparsers)
    return parser


def _add_stockify_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "stockify",
        help="Analyze an original FCPXML, persist candidates, and make review XML.",
    )
    parser.add_argument("input", type=Path, help="Source .fcpxml file or bundle.")
    parser.add_argument("--output", type=Path, help="Generated Final Cut review XML.")
    parser.add_argument("--report", type=Path, help="Stockify JSON report.")
    parser.add_argument("--manifest", type=Path, help="Export/package manifest JSON.")
    parser.add_argument("--db", type=Path, help="SQLite catalog path.")
    parser.add_argument("--library-name", default="VClip Stock Review")
    parser.add_argument(
        "--layout",
        choices=("both", "timeline-batch", "project-per-clip"),
        default="both",
        help="Default 'both' writes one compilation and one project per accepted clip.",
    )
    parser.add_argument(
        "--include-compilations",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep compilation projects when using project-per-clip layout.",
    )
    parser.add_argument(
        "--min-duration",
        type=positive_float,
        default=0.5,
        help="Absolute input floor in seconds. Shorter timeline clips are rejected.",
    )
    parser.add_argument("--max-segments-per-project", type=positive_int)
    parser.add_argument("--project-name", action="append", default=[])
    parser.add_argument("--keep-audio", action="store_true")

    recovery = parser.add_argument_group("short-clip recovery")
    recovery.add_argument(
        "--recover-short-clips",
        action="store_true",
        help=(
            "Attempt to expand clips shorter than --short-clip-threshold using "
            "source handles. Without this flag those shorts are rejected."
        ),
    )
    recovery.add_argument(
        "--short-clip-threshold",
        type=positive_float,
        default=3.0,
        help=(
            "Clips shorter than this require successful recovery before acceptance "
            "(not a soft preference)."
        ),
    )
    recovery.add_argument(
        "--expanded-minimum-duration",
        type=positive_float,
        default=3.0,
        help="Minimum accepted recovered length; also the shortest expansion target.",
    )
    recovery.add_argument(
        "--expanded-preferred-duration",
        type=positive_float,
        default=5.0,
        help="Preferred expansion target tried before the minimum.",
    )
    recovery.add_argument(
        "--expanded-ideal-duration",
        type=positive_float,
        default=10.0,
        help="Ideal expansion target tried first when source handles allow it.",
    )
    recovery.add_argument(
        "--sidecar-root",
        type=Path,
        action="append",
        default=[],
        help="Archive root to scan for exact-stem DJI SRT files. Repeatable.",
    )
    recovery.add_argument(
        "--scan-volumes",
        action="store_true",
        help="Also scan /Volumes. Convenient, but potentially slower.",
    )
    recovery.add_argument("--require-srt-for-expansion", action="store_true")

    visual = parser.add_argument_group("optional frame-motion scoring")
    visual.add_argument("--visual-score", action="store_true")
    visual.add_argument("--require-visual-for-expansion", action="store_true")
    visual.add_argument("--visual-fps", type=positive_int, default=12)
    visual.add_argument("--visual-width", type=positive_int, default=320)
    visual.add_argument("--visual-height", type=positive_int, default=180)
    visual.add_argument("--visual-reject-shift-px", type=positive_float, default=12.0)
    visual.add_argument("--visual-reject-frame-diff", type=positive_float, default=12.0)
    visual.add_argument("--visual-timeout", type=positive_float, default=120.0)

    policy = parser.add_argument_group("eligibility policy")
    policy.add_argument("--require-camera-lut", action="store_true")
    policy.add_argument("--require-custom-lut", action="store_true")

    organization = parser.add_argument_group("session organization")
    organization.add_argument("--session-gap-hours", type=positive_float, default=4.0)
    organization.add_argument(
        "--places-file",
        type=Path,
        help="Optional JSON place catalog. Defaults to the bundled catalog.",
    )
    organization.add_argument(
        "--location-provider",
        choices=("catalog", "catalog+nominatim"),
        default="catalog+nominatim",
        help=(
            "Resolve GPS via the local catalog first, then cached Nominatim "
            "(default). Use catalog for offline-only resolution."
        ),
    )
    organization.add_argument(
        "--nominatim-user-agent",
        help=(
            "Optional override for Nominatim identity. Otherwise uses "
            "VCLIP_NOMINATIM_USER_AGENT, ~/.config/vclip/config.json "
            "(or macOS Application Support), then the built-in default."
        ),
    )

    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--inspect-assets", action="store_true")
    parser.set_defaults(handler=_run_stockify)


def _add_reconcile_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "reconcile",
        help="Record human trims, deletions, and treatment changes from reviewed XML.",
    )
    parser.add_argument("reviewed_xml", type=Path)
    parser.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    parser.add_argument("--run-id")
    parser.add_argument(
        "--authority",
        choices=("auto", "compilation", "individual"),
        default="auto",
        help=(
            "auto (default): individual projects are authoritative when Stockify "
            "generated them; Stock Compilation is informational. Use compilation "
            "only for compilation-only review/export workflows."
        ),
    )
    parser.add_argument(
        "--scope",
        choices=("observed-projects", "full-run"),
        default="observed-projects",
        help="Use full-run only when the XML definitely contains the whole review library.",
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--allow-conflicts", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.set_defaults(handler=_run_reconcile)


def _add_package_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "package",
        help="Match exported MP4s to the DB and build metadata-rich package folders.",
    )
    parser.add_argument("exports_directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    parser.add_argument("--run-id")
    parser.add_argument("--project-label", action="append", default=[])
    parser.add_argument(
        "--mode",
        choices=("copy", "move", "hardlink", "symlink"),
        default="copy",
    )
    parser.add_argument(
        "--weather",
        choices=("open-meteo", "none"),
        default="open-meteo",
        help=(
            "Historical weather enrichment provider. Default open-meteo uses the "
            "Open-Meteo archive API with session GPS + capture time. Use 'none' to opt out."
        ),
    )
    parser.add_argument(
        "--require-weather",
        action="store_true",
        help="Fail packaging when weather enrichment is unavailable or fails.",
    )
    parser.add_argument("--no-checksum", action="store_true")
    parser.add_argument("--no-probe", action="store_true")
    parser.add_argument("--allow-unmatched", action="store_true")
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--allow-duration-mismatch", action="store_true")
    parser.add_argument("--allow-unreconciled", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--quiet", action="store_true")
    parser.set_defaults(handler=_run_package)


def _add_recover_locations_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "recover-locations",
        help=(
            "Recover Unknown Location sessions from SRT GPS, same-shoot DJI "
            "JPG EXIF inference, and optionally rewrite review XML names."
        ),
    )
    parser.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    parser.add_argument(
        "--run-id",
        help=(
            "Limit recovery to one Stockify run. "
            "Default with --db is catalog-wide across all complete runs."
        ),
    )
    parser.add_argument(
        "--session-id",
        help="Limit recovery to one shoot session id.",
    )
    parser.add_argument("--report", type=Path, help="Optional JSON report path.")
    parser.add_argument(
        "--rewrite-review-xml",
        action="store_true",
        help=(
            "Update event/project names in every affected Stockify review XML "
            "without rerunning extraction."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute recovery results without writing SQLite or review XML changes.",
    )
    parser.add_argument(
        "--refresh-resolved",
        action="store_true",
        help=(
            "Re-resolve sessions that already have a location from live flight GPS "
            "(useful after place-hierarchy fixes)."
        ),
    )
    parser.add_argument("--session-gap-hours", type=positive_float, default=4.0)
    parser.add_argument(
        "--scan",
        type=Path,
        action="append",
        default=[],
        help=(
            "Root to scan for SRT sidecars and same-shoot DJI JPG/JPEG stills "
            "when catalog paths are missing. Repeatable. Defaults to /Volumes."
        ),
    )
    parser.add_argument(
        "--no-jpg-exif-recovery",
        action="store_true",
        help="Disable same-shoot JPG EXIF GPS fallback (SRT/catalog GPS only).",
    )
    parser.add_argument(
        "--places-file",
        type=Path,
        help="Optional JSON place catalog. Defaults to the bundled catalog.",
    )
    parser.add_argument(
        "--location-provider",
        choices=("catalog", "catalog+nominatim"),
        default="catalog+nominatim",
        help=(
            "Resolve GPS via the local catalog first, then cached Nominatim "
            "(default). Use catalog for offline-only resolution."
        ),
    )
    parser.add_argument(
        "--nominatim-user-agent",
        help=(
            "Optional override for Nominatim identity. Otherwise uses "
            "VCLIP_NOMINATIM_USER_AGENT, ~/.config/vclip/config.json "
            "(or macOS Application Support), then the built-in default."
        ),
    )
    parser.add_argument("--quiet", action="store_true")
    parser.set_defaults(handler=_run_recover_locations)


def _add_diagnose_locations_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "diagnose-locations",
        help=(
            "Explain remaining Unknown Location sessions and what media/SRTs "
            "may still be on another drive, SD card, or drone."
        ),
    )
    parser.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    parser.add_argument(
        "--scan",
        type=Path,
        action="append",
        default=[],
        help=(
            "Root to scan for currently available media/SRT files and same-shoot "
            "DJI JPG/JPEG stills. Repeatable. Defaults to /Volumes."
        ),
    )
    parser.add_argument(
        "--places-file",
        type=Path,
        help="Optional JSON place catalog. Defaults to the bundled catalog.",
    )
    parser.add_argument(
        "--location-provider",
        choices=("catalog", "catalog+nominatim"),
        default="catalog+nominatim",
        help="Place resolver used for GPS trajectory analysis.",
    )
    parser.add_argument("--nominatim-user-agent", help="Optional Nominatim identity override.")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-session source filenames and detailed evidence.",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.set_defaults(handler=_run_diagnose_locations)


def _add_libraries_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "libraries",
        help="Show Final Cut libraries VClip has processed, optionally vs a drive scan.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("vclip.sqlite3"),
        help="SQLite catalog path (default: ./vclip.sqlite3).",
    )
    parser.add_argument(
        "--scan",
        type=Path,
        help="Scan a folder/drive for .fcpbundle libraries and compare against processed records.",
    )
    parser.add_argument(
        "--xml-dir",
        type=Path,
        help=(
            "Also scan a folder for .fcpxml/.fcpxmld exports and report XML found/missing "
            "per library (separate from processed state)."
        ),
    )
    parser.set_defaults(handler=_run_libraries)


def _add_db_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("db", help="Inspect or initialize the SQLite catalog.")
    db_subparsers = parser.add_subparsers(dest="db_command", required=True)
    status = db_subparsers.add_parser("status")
    status.add_argument("--db", type=Path, default=Path("vclip.sqlite3"))
    status.set_defaults(handler=_run_db_status)


def _progress(quiet: bool):
    started = time.monotonic()

    def emit(message: str) -> None:
        if not quiet:
            print(f"[{time.monotonic() - started:7.1f}s] {message}", flush=True)

    return emit


def _database(path: Path) -> tuple[Database, CatalogRepository]:
    database = Database(path)
    database.migrate()
    return database, CatalogRepository(database)


def _run_stockify(args: argparse.Namespace) -> int:
    requested = args.input.expanduser().resolve()
    input_path, messages = resolve_input_fcpxml(requested)
    for message in messages:
        print(message)
    if args.inspect_assets:
        return print_asset_diagnostics(input_path)
    if args.validate_only:
        return print_validation_report(input_path)

    output = (
        args.output.expanduser().resolve()
        if args.output
        else requested.with_name(f"{requested.stem}-stock-review.fcpxml")
    )
    report = (
        args.report.expanduser().resolve()
        if args.report
        else output.with_name(f"{output.stem}-report.json")
    )
    manifest = (
        args.manifest.expanduser().resolve()
        if args.manifest
        else output.with_name(f"{output.stem}-export-manifest.json")
    )
    db_path = args.db.expanduser().resolve() if args.db else output.parent / "vclip.sqlite3"
    if output == input_path:
        raise VClipError("The output XML must not overwrite the source XML.")

    _, repository = _database(db_path)
    places_path = (
        args.places_file.expanduser().resolve() if args.places_file else default_places_path()
    )
    location_resolver = build_location_resolver(
        repository,
        places_path=places_path,
        enable_nominatim=args.location_provider == "catalog+nominatim",
        nominatim_user_agent_override=args.nominatim_user_agent,
    )
    sidecar_roots = [path.expanduser().resolve() for path in args.sidecar_root]
    if args.scan_volumes:
        sidecar_roots.append(Path("/Volumes"))

    options = StockifyOptions(
        input_path=input_path,
        requested_path=requested,
        output_path=output,
        report_path=report,
        database_path=db_path,
        manifest_path=manifest,
        library_name=args.library_name,
        layout=args.layout,
        include_compilations=args.include_compilations,
        min_duration_seconds=args.min_duration,
        max_segments_per_project=args.max_segments_per_project,
        force_disable_audio=not args.keep_audio,
        recover_short_clips=args.recover_short_clips,
        short_clip_threshold_seconds=args.short_clip_threshold,
        expanded_minimum_duration_seconds=args.expanded_minimum_duration,
        expanded_preferred_duration_seconds=args.expanded_preferred_duration,
        expanded_ideal_duration_seconds=args.expanded_ideal_duration,
        sidecar_roots=tuple(sidecar_roots),
        require_srt_for_expansion=args.require_srt_for_expansion,
        visual_score=args.visual_score,
        require_visual_for_expansion=args.require_visual_for_expansion,
        visual_fps=args.visual_fps,
        visual_width=args.visual_width,
        visual_height=args.visual_height,
        visual_reject_shift_px=args.visual_reject_shift_px,
        visual_reject_frame_diff=args.visual_reject_frame_diff,
        visual_timeout_seconds=args.visual_timeout,
        require_camera_lut=args.require_camera_lut,
        require_custom_lut=args.require_custom_lut,
        project_names=frozenset(args.project_name) if args.project_name else None,
        session_gap_hours=args.session_gap_hours,
    )
    service = StockifyService(
        repository,
        location_resolver,
        progress=_progress(args.quiet),
    )
    result = service.run(options)
    print()
    print("Stockify complete")
    print("-----------------")
    print(f"Run ID:              {result.stockify_run_id}")
    print(f"Review XML:          {result.output_file}")
    print(f"Database:            {result.database_file}")
    print(f"Report:              {report}")
    print(f"Manifest:            {manifest}")
    print(f"Shoot sessions:      {result.shoot_sessions_generated}")
    print(f"Accepted candidates: {result.segment_summary.written}")
    print(f"Rejected candidates: {result.segment_summary.skipped}")
    print(f"Compilation projects:{result.compilation_projects_written:>6}")
    print("Import the review XML into a TEST Final Cut library.")
    return 0


def _run_reconcile(args: argparse.Namespace) -> int:
    reviewed_xml = args.reviewed_xml.expanduser().resolve()
    _, repository = _database(args.db.expanduser().resolve())
    report_path = (
        args.report.expanduser().resolve()
        if args.report
        else reviewed_xml.with_name(f"{reviewed_xml.stem}-reconcile-report.json")
    )
    service = ReconcileService(repository, progress=_progress(args.quiet))
    result = service.run(
        reviewed_xml=reviewed_xml,
        run_id=args.run_id,
        authority=args.authority,
        scope=args.scope,
        report_path=report_path,
        allow_conflicts=args.allow_conflicts,
    )
    print()
    print("Reconcile complete")
    print("------------------")
    print(f"Run ID:      {result.stockify_run_id}")
    print(f"Approved:    {result.approved}")
    print(f"Rejected:    {result.rejected}")
    print(f"Modified:    {result.modified}")
    print(f"Conflicts:   {result.conflicts}")
    print(f"Out of scope:{result.out_of_scope:>6}")
    print(f"Report:      {report_path}")
    print("No XML needs to be imported back into Final Cut.")
    return 0


def _run_package(args: argparse.Namespace) -> int:
    _, repository = _database(args.db.expanduser().resolve())
    weather_provider = (
        OpenMeteoHistoricalWeatherProvider()
        if args.weather == "open-meteo"
        else NoWeatherProvider()
    )
    output = args.output.expanduser().resolve()
    report_path = (
        args.report.expanduser().resolve()
        if args.report
        else output.parent / f"{output.name}-package-report.json"
    )
    service = PackageService(repository, progress=_progress(args.quiet))
    result = service.run(
        exports_directory=args.exports_directory.expanduser().resolve(),
        output_directory=output,
        run_id=args.run_id,
        project_labels=set(args.project_label) if args.project_label else None,
        mode=args.mode,
        weather_provider=weather_provider,
        calculate_checksums=not args.no_checksum,
        inspect_media=not args.no_probe,
        allow_unmatched=args.allow_unmatched,
        allow_missing=args.allow_missing,
        allow_duration_mismatch=args.allow_duration_mismatch,
        allow_unreconciled=args.allow_unreconciled,
        require_weather=args.require_weather,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        report_path=report_path,
    )
    print()
    print("Package complete")
    print("----------------")
    print(f"Run ID:          {result.stockify_run_id}")
    print(f"Matched exports: {result.exports_matched}")
    print(f"Packages:        {result.packages_created}")
    print(f"Output:          {result.output_directory}")
    print(f"Report:          {report_path}")
    if result.missing_candidate_ids:
        print(f"Missing exports: {len(result.missing_candidate_ids)}")
    return 0


def _run_recover_locations(args: argparse.Namespace) -> int:
    _, repository = _database(args.db.expanduser().resolve())
    places_path = (
        args.places_file.expanduser().resolve() if args.places_file else default_places_path()
    )
    location_resolver = build_location_resolver(
        repository,
        places_path=places_path,
        enable_nominatim=args.location_provider == "catalog+nominatim",
        nominatim_user_agent_override=args.nominatim_user_agent,
    )
    report_path = args.report.expanduser().resolve() if args.report else None
    scan_roots = (
        [path.expanduser().resolve() for path in args.scan] if args.scan else [Path("/Volumes")]
    )
    service = LocationRecoveryService(
        repository,
        location_resolver,
        session_gap_hours=args.session_gap_hours,
        scan_roots=scan_roots,
        progress=_progress(args.quiet),
    )
    result = service.run(
        run_id=args.run_id,
        dry_run=args.dry_run,
        rewrite_review_xml=args.rewrite_review_xml,
        report_path=report_path,
        refresh_resolved=args.refresh_resolved,
        enable_jpg_exif=not args.no_jpg_exif_recovery,
        session_id=args.session_id,
    )
    print()
    print("Location recovery")
    print("-----------------")
    for line in format_location_recovery_report(result):
        print(line)
    if args.run_id:
        print(f"Run ID filter:             {args.run_id}")
    if args.session_id:
        print(f"Session ID filter:         {args.session_id}")
    if result.rewritten_review_xmls:
        for path in result.rewritten_review_xmls:
            print(f"Review XML rewritten:      {path}")
    if report_path is not None:
        print(f"Report:                    {report_path}")
    for warning in result.warnings:
        print(f"warning: {warning}")
    return 0


def _run_diagnose_locations(args: argparse.Namespace) -> int:
    _, repository = _database(args.db.expanduser().resolve())
    scan_roots = (
        [path.expanduser().resolve() for path in args.scan] if args.scan else [Path("/Volumes")]
    )
    places_path = (
        args.places_file.expanduser().resolve() if args.places_file else default_places_path()
    )
    location_resolver = build_location_resolver(
        repository,
        places_path=places_path,
        enable_nominatim=args.location_provider == "catalog+nominatim",
        nominatim_user_agent_override=args.nominatim_user_agent,
    )
    service = LocationDiagnosticsService(
        repository,
        location_resolver,
        scan_roots=scan_roots,
        progress=_progress(args.quiet),
    )
    report = service.run(verbose=args.verbose)
    for line in format_location_diagnostics_report(report, verbose=args.verbose):
        print(line)
    return 0


def _run_libraries(args: argparse.Namespace) -> int:
    _, repository = _database(args.db.expanduser().resolve())
    processed = repository.processed_libraries()
    scanned = None
    if args.scan is not None:
        scan_root = args.scan.expanduser().resolve()
        if not scan_root.exists():
            raise VClipError(f"Scan path does not exist: {scan_root}")
        scanned = find_fcpbundles(scan_root)
    xml_library_names = None
    if args.xml_dir is not None:
        xml_root = args.xml_dir.expanduser().resolve()
        if not xml_root.is_dir():
            raise VClipError(f"XML directory does not exist: {xml_root}")
        xml_library_names = discover_xml_library_names(xml_root)
    lines = format_libraries_report(
        processed=processed,
        scanned=scanned,
        xml_library_names=xml_library_names,
    )
    if not lines:
        if scanned is not None:
            print("No Final Cut libraries (.fcpbundle) found in the scan path.")
        else:
            print("No processed Final Cut libraries recorded yet.")
        return 0
    for line in lines:
        print(line)
    if xml_library_names is not None:
        xml_found = sum(1 for line in lines if line.endswith("XML found"))
        xml_missing = sum(1 for line in lines if line.endswith("XML missing"))
        print()
        print(f"Libraries: {len(lines)}")
        print(f"XML found: {xml_found}")
        print(f"XML missing: {xml_missing}")
    elif scanned is not None:
        remaining = sum(1 for line in lines if line.startswith("○"))
        print()
        print(f"Remaining: {remaining}")
    return 0


def _run_db_status(args: argparse.Namespace) -> int:
    database, repository = _database(args.db.expanduser().resolve())
    print(f"Database: {database.path}")
    for table, count in repository.database_status().items():
        print(f"{table:20} {count}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.handler(args))
    except (VClipError, StockifyError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def stockify_entry() -> int:
    return main(["stockify", *sys.argv[1:]])


def reconcile_entry() -> int:
    return main(["reconcile", *sys.argv[1:]])


def package_entry() -> int:
    return main(["package", *sys.argv[1:]])
